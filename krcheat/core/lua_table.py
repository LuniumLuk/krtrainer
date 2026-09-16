"""Lossless reader and writer for the game's save grammar (foundation §12, decision D1).

The save files are plain-text Lua chunks:

    local obj1 = {
    \t["achievement_counters"] = {
    \t\t["ACDC"] = 273;
    \t};
    \t["gems"] = 4154;
    \t["version_string"] = "kr1-desktop-6.4.46";
    }
    return obj1

with an optional `multiRefObjects` preamble when the table graph has aliases.

Why this is a *parser* and not an `eval`-alike
-----------------------------------------------
Every scalar node keeps the exact source text that produced it, and every table keeps
the byte range it occupied. Render therefore re-emits untouched bytes **verbatim** and
only renders nodes the tool actually changed. The consequence is the load-bearing
safety property of tier 1 (§12.2):

    a command that changes nothing produces a byte-identical file.

That is why we never re-serialise floats from Python. The game writes with Lua's
default `%.14g` (visible in values like `0.021276595745681`), so a re-render cannot
round-trip a double; literal preservation sidesteps the problem for every value we did
not touch. Values we *do* introduce are rendered from Python, and the game truncates
them to `%.14g` on its next save, which is harmless.

Nothing here knows about Kingdom Rush or about slots. It is a codec.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "LuaTableError",
    "Scalar",
    "Table",
    "Entry",
    "Document",
    "parse",
    "parse_value",
    "render_scalar",
    "flatten_paths",
    "MANDATORY_TOP_LEVEL",
    "EXPECTED_TOP_LEVEL",
]


class LuaTableError(Exception):
    """The text is not in the grammar, or an edit would violate the schema.

    A subclass of `Exception` rather than `KrcheatError` so the codec stays usable
    from tests and from `core.data` without dragging the exit-code contract in.
    """


#: Keys the game's storage layer is known to validate a slot against (§5.1, §5.3).
#: We never *require* them on read (a partially written file must still be inspectable)
#: but the write path refuses to produce a file that lost one.
MANDATORY_TOP_LEVEL = (
    "version_string",
    "upgrades",
    "levels",
)

#: Keys observed in a real slot (§5.3). Missing ones are reported as warnings.
EXPECTED_TOP_LEVEL = (
    "version_string",
    "gems",
    "difficulty",
    "upgrades",
    "levels",
    "heroes",
    "achievements",
    "achievement_counters",
    "seen",
)

_WS = " \t\r\n"
_IDENT_EXTRA = "_"
_DIGITS = "0123456789"


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class Node(object):
    """A value in the tree, with the byte range it occupied in the source."""

    __slots__ = ("start", "end", "parent")

    kind = "node"

    def __init__(self, start=-1, end=-1, parent=None):
        self.start = start
        self.end = end
        self.parent = parent

    @property
    def synthetic(self):
        """True when this node was created by an edit and has no source text."""
        return self.start < 0

    def as_python(self):  # pragma: no cover - overridden
        raise NotImplementedError


class Scalar(Node):
    """A number, string or boolean. `text` is exactly what will be emitted."""

    __slots__ = ("text", "kind")

    def __init__(self, text, kind, start=-1, end=-1, parent=None):
        Node.__init__(self, start, end, parent)
        self.text = text
        self.kind = kind  # 'number' | 'string' | 'bool'

    def as_python(self):
        if self.kind == "bool":
            return self.text == "true"
        if self.kind == "string":
            return unescape(self.text)
        return parse_number(self.text)

    def as_int(self):
        value = self.as_python()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LuaTableError("{0} is not a number".format(repr(value)))
        return int(value)

    def as_str(self):
        value = self.as_python()
        if not isinstance(value, str):
            raise LuaTableError("{0} is not a string".format(repr(value)))
        return value

    def as_bool(self):
        value = self.as_python()
        if not isinstance(value, bool):
            raise LuaTableError("{0} is not a boolean".format(repr(value)))
        return value

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Scalar({0!r}, {1})".format(self.text, self.kind)


class Table(Node):
    """An ordered mapping. Order is the file's order and is never changed."""

    __slots__ = ("entries", "dirty")

    kind = "table"

    def __init__(self, start=-1, end=-1, parent=None, entries=None, dirty=False):
        Node.__init__(self, start, end, parent)
        self.entries = list(entries or ())
        self.dirty = dirty

    # -- lookup --------------------------------------------------------------

    def find(self, key):
        for entry in self.entries:
            if entry.key == key and type(entry.key) is type(key):
                return entry
        # int/float key equivalence (`[1]` parsed as int, asked for as 1.0)
        for entry in self.entries:
            if entry.key == key:
                return entry
        return None

    def get(self, key):
        entry = self.find(key)
        return None if entry is None else entry.value

    def keys(self):
        return [entry.key for entry in self.entries]

    def as_python(self):
        out = {}
        for entry in self.entries:
            out[entry.key] = entry.value.as_python()
        return out

    # -- mutation ------------------------------------------------------------

    def set(self, key, node):
        """Replace an existing value or insert a new entry; returns the Entry."""
        entry = self.find(key)
        if entry is not None:
            entry.value.parent = self
            entry.value = node
            self.mark_dirty()
            return entry
        entry = Entry(key_text=render_key(key), key=key, value=node)
        node.parent = self
        index = self._insert_index(key)
        self.entries.insert(index, entry)
        self.mark_dirty()
        return entry

    def _insert_index(self, key):
        """Where a *new* key goes.

        Existing entries are never reordered (§12.2). New numeric keys are inserted in
        ascending numeric order so a hand-edited file stays readable; anything else is
        appended.
        """
        if isinstance(key, bool):
            return len(self.entries)
        if isinstance(key, int) and all(type(e.key) is int for e in self.entries):
            for index, entry in enumerate(self.entries):
                if entry.key > key:
                    return index
            return len(self.entries)
        return len(self.entries)

    def mark_dirty(self):
        node = self
        while node is not None and not node.dirty:
            node.dirty = True
            node = node.parent

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Table({0} entries{1})".format(len(self.entries), ", dirty" if self.dirty else "")


class Entry(object):
    """A `["key"] = value;` entry. `key_text` includes the brackets."""

    __slots__ = ("key_text", "key", "value", "start", "end")

    def __init__(self, key_text, key, value, start=-1, end=-1):
        self.key_text = key_text
        self.key = key
        self.value = value
        self.start = start
        self.end = end

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Entry({0})".format(self.key_text)


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------


def parse_number(text):
    """Parse a Lua numeric literal to int or float, preserving intness."""
    body = text.strip()
    lowered = body.lower()
    try:
        if lowered.startswith("0x") or lowered.startswith("-0x"):
            return int(body, 16)
        if "." in body or "e" in lowered:
            return float(body)
        return int(body)
    except ValueError:
        raise LuaTableError("not a numeric literal: {0!r}".format(text))


def _escape(text):
    out = ['"']
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 32 or ord(char) == 127:
            out.append("\\{0:03d}".format(ord(char)))
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "\n": "\n",
}


def unescape(literal):
    """Turn a quoted Lua string literal into its Python value."""
    body = literal.strip()
    if body.startswith("[") and body.endswith("]"):
        # long string [[ ... ]] / [=[ ... ]=]
        level = 0
        while 1 + level < len(body) and body[1 + level] == "=":
            level += 1
        inner = body[2 + level : len(body) - (2 + level)]
        return inner
    if len(body) >= 2 and body[0] in "\"'" and body[-1] == body[0]:
        body = body[1:-1]
    out = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise LuaTableError("trailing backslash in string literal")
        nxt = body[index]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            index += 1
        elif nxt.isdigit():
            digits = ""
            while index < len(body) and len(digits) < 3 and body[index].isdigit():
                digits += body[index]
                index += 1
            out.append(chr(int(digits, 10)))
        elif nxt == "x":
            hexits = body[index + 1 : index + 3]
            if len(hexits) != 2:
                raise LuaTableError("bad \\x escape in string literal")
            out.append(chr(int(hexits, 16)))
            index += 3
        else:
            raise LuaTableError("unknown escape \\{0} in string literal".format(nxt))
    return "".join(out)


def render_key(key):
    """`["name"]` or `[7]`, matching the style the game emits."""
    if isinstance(key, bool):
        raise LuaTableError("boolean table keys are not supported")
    if isinstance(key, int):
        return "[{0}]".format(key)
    if isinstance(key, float):
        return "[{0}]".format(repr(key))
    return "[{0}]".format(_escape(str(key)))


def render_scalar(value):
    """Render a Python scalar as the literal the game's writer would emit."""
    if isinstance(value, bool):
        return Scalar("true" if value else "false", "bool")
    if isinstance(value, int):
        return Scalar(str(value), "number")
    if isinstance(value, float):
        # §12.2: values we introduce use Python's shortest round-trip form.
        return Scalar(repr(value), "number")
    if isinstance(value, str):
        return Scalar(_escape(value), "string")
    raise LuaTableError("cannot render {0!r} as a scalar".format(value))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _Parser(object):
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.length = len(text)

    # -- primitives ----------------------------------------------------------

    def fail(self, message):
        line = self.text.count("\n", 0, self.pos) + 1
        raise LuaTableError("{0} (line {1}, offset {2})".format(message, line, self.pos))

    def eof(self):
        return self.pos >= self.length

    def peek(self, count=1):
        return self.text[self.pos : self.pos + count]

    def skip_ws(self):
        while self.pos < self.length:
            char = self.text[self.pos]
            if char in _WS:
                self.pos += 1
            elif char == "-" and self.peek(2) == "--":
                if self.peek(4) == "--[[":  # block comment
                    end = self.text.find("]]", self.pos + 4)
                    self.pos = self.length if end < 0 else end + 2
                else:
                    end = self.text.find("\n", self.pos)
                    self.pos = self.length if end < 0 else end
            else:
                break

    def take_word(self, word):
        if self.text.startswith(word, self.pos) and not self._ident_continues(self.pos + len(word)):
            self.pos += len(word)
            return True
        return False

    def expect_word(self, word):
        if not self.take_word(word):
            self.fail("expected {0!r}".format(word))

    def _ident_continues(self, index):
        return index < self.length and (self.text[index].isalnum() or self.text[index] in _IDENT_EXTRA)

    def take_ident(self):
        start = self.pos
        while self.pos < self.length and (
            self.text[self.pos].isalnum() or self.text[self.pos] in _IDENT_EXTRA
        ):
            self.pos += 1
        if start == self.pos:
            self.fail("expected an identifier")
        return self.text[start : self.pos]

    def expect(self, char):
        if self.peek() != char:
            self.fail("expected {0!r}".format(char))
        self.pos += 1

    # -- values --------------------------------------------------------------

    def parse_value(self):
        char = self.peek()
        if char == "{":
            return self.parse_table()
        if char in "\"'":
            return self.parse_short_string()
        if char == "[":
            return self.parse_long_string()
        if char in "-" + _DIGITS or (char == "." and self.peek(2)[1:2].isdigit()):
            return self.parse_number()
        if self.text.startswith("true", self.pos):
            start = self.pos
            self.pos += 4
            return Scalar("true", "bool", start, self.pos)
        if self.text.startswith("false", self.pos):
            start = self.pos
            self.pos += 5
            return Scalar("false", "bool", start, self.pos)
        if self.text.startswith("nil", self.pos):
            # The game never writes nil, and a nil entry is not the same as an absent
            # one. Treated as an error so a surprise is loud rather than lossy.
            self.fail("nil entries are not part of the save grammar")
        self.fail("unexpected character {0!r}".format(char))

    def parse_number(self):
        start = self.pos
        if self.peek() in "+-":
            self.pos += 1
        if self.peek(2).lower() == "0x":
            self.pos += 2
            while self.pos < self.length and self.text[self.pos] in "0123456789abcdefABCDEF":
                self.pos += 1
        else:
            while self.pos < self.length and self.text[self.pos] in _DIGITS:
                self.pos += 1
            if self.peek() == ".":
                self.pos += 1
                while self.pos < self.length and self.text[self.pos] in _DIGITS:
                    self.pos += 1
            if self.peek().lower() == "e":
                self.pos += 1
                if self.peek() in "+-":
                    self.pos += 1
                while self.pos < self.length and self.text[self.pos] in _DIGITS:
                    self.pos += 1
        literal = self.text[start : self.pos]
        if not literal:
            self.fail("empty numeric literal")
        try:
            parse_number(literal)
        except LuaTableError:
            self.fail("bad numeric literal {0!r}".format(literal))
        return Scalar(literal, "number", start, self.pos)

    def parse_short_string(self):
        start = self.pos
        quote = self.text[self.pos]
        self.pos += 1
        while True:
            if self.pos >= self.length:
                self.fail("unterminated string")
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 2
                continue
            if char == quote:
                self.pos += 1
                break
            if char == "\n":
                self.fail("newline inside a short string")
            self.pos += 1
        literal = self.text[start : self.pos]
        unescape(literal)  # validate now, so a bad escape fails at read time
        return Scalar(literal, "string", start, self.pos)

    def parse_long_string(self):
        start = self.pos
        level = 0
        if self.text.startswith("[[", self.pos):
            pass
        elif self.text.startswith("[=", self.pos):
            while self.pos + 1 + level < self.length and self.text[self.pos + 1 + level] == "=":
                level += 1
        else:
            self.fail("unexpected '['")
        opener = "[" + "=" * level + "["
        closer = "]" + "=" * level + "]"
        if not self.text.startswith(opener, self.pos):
            self.fail("malformed long string opener")
        end = self.text.find(closer, self.pos + len(opener))
        if end < 0:
            self.fail("unterminated long string")
        self.pos = end + len(closer)
        return Scalar(self.text[start : self.pos], "string", start, self.pos)

    def parse_table(self):
        start = self.pos
        self.expect("{")
        entries = []
        while True:
            self.skip_ws()
            if self.peek() == "}":
                self.pos += 1
                break
            if self.eof():
                self.fail("unterminated table")
            entry_start = self.pos
            self.expect("[")
            self.skip_ws()
            if self.peek() in "\"'":
                key_node = self.parse_short_string()
            else:
                key_node = self.parse_number()
            self.skip_ws()
            self.expect("]")
            key_text = self.text[entry_start : self.pos]
            key = key_node.as_python()
            self.skip_ws()
            self.expect("=")
            self.skip_ws()
            value_start = self.pos
            value = self.parse_value()
            self.skip_ws()
            if self.peek() not in ";,":
                self.fail("expected ';' after a table entry")
            self.pos += 1
            entry = Entry(key_text, key, value, entry_start, self.pos)
            value.parent = None
            entries.append(entry)
        table = Table(start, self.pos, entries=entries)
        for entry in table.entries:
            entry.value.parent = table
        return table


def parse_value(text):
    """Parse a single value (used for `--value` style inputs and by tests)."""
    parser = _Parser(text)
    parser.skip_ws()
    node = parser.parse_value()
    parser.skip_ws()
    if not parser.eof():
        parser.fail("trailing content")
    return node


def parse(text):
    """Parse a whole save chunk into a `Document`."""
    if not isinstance(text, str):
        raise LuaTableError("expected text, got {0}".format(type(text).__name__))
    parser = _Parser(text)
    decls = {}
    returned = None
    while True:
        parser.skip_ws()
        if parser.eof():
            break
        if parser.take_word("local"):
            parser.skip_ws()
            name = parser.take_ident()
            parser.skip_ws()
            parser.expect("=")
            parser.skip_ws()
            decls[name] = parser.parse_value()
            continue
        if parser.take_word("return"):
            parser.skip_ws()
            returned = parser.take_ident()
            break
        parser.fail("expected 'local' or 'return'")
    if returned is None:
        raise LuaTableError("no 'return' statement: this is not a save chunk")
    if returned not in decls:
        raise LuaTableError("returns {0!r}, which was never declared".format(returned))
    root = decls[returned]
    if not isinstance(root, Table):
        raise LuaTableError("the returned value is not a table")
    return Document(text=text, root=root, root_name=returned, decls=decls)


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def split_path(path):
    """`levels.1.stars` -> `['levels', 1, 'stars']`."""
    if isinstance(path, (list, tuple)):
        return list(path)
    parts = []
    for raw in str(path).split("."):
        segment = raw.strip()
        if not segment:
            raise LuaTableError("empty path segment in {0!r}".format(path))
        if segment.lstrip("-").isdigit():
            parts.append(int(segment))
        else:
            parts.append(segment)
    return parts


def flatten_paths(value, prefix=""):
    """Every addressable path in a Python value, for the no-deletion check (§12.3)."""
    paths = set()
    if isinstance(value, dict):
        for key, item in value.items():
            child = "{0}.{1}".format(prefix, key) if prefix else str(key)
            paths.add(child)
            paths |= flatten_paths(item, child)
    return paths


class Document(object):
    """A parsed save: the AST, the original text, and the edits made to it."""

    def __init__(self, text, root, root_name="obj1", decls=None):
        self.text = text
        self.root = root
        self.root_name = root_name
        self.decls = decls or {}
        #: (path, before, after) for every change an operation made
        self.changes = []

    # -- reading -------------------------------------------------------------

    def python(self):
        return self.root.as_python()

    def resolve(self, path):
        """Return (parent_table, key, value_node_or_None) for a path."""
        parts = split_path(path)
        table = self.root
        for index, part in enumerate(parts[:-1]):
            node = table.get(part)
            if node is None:
                return None, None, None
            if not isinstance(node, Table):
                raise LuaTableError(
                    "{0} is a {1}, not a table (at '{2}')".format(
                        ".".join(str(p) for p in parts[: index + 1]), node.kind, path
                    )
                )
            table = node
        key = parts[-1]
        entry = table.find(key)
        return table, key, (None if entry is None else entry.value)

    def get(self, path):
        """Read a dotted path as a Python value; raises when it does not exist."""
        _table, _key, node = self.resolve(path)
        if node is None:
            raise LuaTableError("no such path: {0}".format(path))
        return node.as_python()

    def has(self, path):
        _table, _key, node = self.resolve(path)
        return node is not None

    def keys_at(self, path):
        _table, _key, node = self.resolve(path)
        if node is None or not isinstance(node, Table):
            return []
        return node.keys()

    def keys(self):
        """Top-level keys, in file order."""
        return self.root.keys()

    # -- writing -------------------------------------------------------------

    def set(self, path, value, create=False):
        """Set a scalar at `path`.

        Returns True when something actually changed. Setting a value to what it
        already is leaves the tree clean, which is what keeps the byte-identity
        guarantee (§12.2) honest rather than incidental.
        """
        parts = split_path(path)
        if not parts:
            raise LuaTableError("empty path")
        parent = self.root
        created = False
        for index in range(len(parts) - 1):
            key = parts[index]
            node = parent.get(key)
            if node is None:
                if not create:
                    raise LuaTableError(
                        "no such table: {0}".format(".".join(str(p) for p in parts[: index + 1]))
                    )
                node = Table(dirty=True, parent=parent)
                parent.set(key, node)
                created = True
            if not isinstance(node, Table):
                raise LuaTableError(
                    "{0} is not a table".format(".".join(str(p) for p in parts[: index + 1]))
                )
            parent = node
        key = parts[-1]
        existing = parent.find(key)
        if existing is None and not create:
            raise LuaTableError("no such path: {0}".format(path))
        before = None if existing is None else existing.value.as_python()
        if existing is not None and before == value and not created:
            return False
        node = render_scalar(value)
        parent.set(key, node)
        self.changes.append((path, before, value))
        return True

    def set_node(self, path, node, create=True):
        """Set a pre-built node (used by `data`-style structured writes)."""
        parts = split_path(path)
        parent = self.root
        for index in range(len(parts) - 1):
            child = parent.get(parts[index])
            if child is None:
                if not create:
                    raise LuaTableError("no such table: {0}".format(parts[index]))
                child = Table(dirty=True, parent=parent)
                parent.set(parts[index], child)
            parent = child
        before = parent.get(parts[-1])
        before_value = None if before is None else before.as_python()
        node.parent = parent
        parent.set(parts[-1], node)
        self.changes.append((".".join(str(p) for p in parts), before_value, node.as_python()))
        return True

    def ensure_table(self, path):
        """Create (if needed) and return the table at `path`."""
        parts = split_path(path)
        parent = self.root
        for index, key in enumerate(parts):
            child = parent.get(key)
            if child is None:
                child = Table(dirty=True, parent=parent)
                parent.set(key, child)
            if not isinstance(child, Table):
                raise LuaTableError(
                    "{0} is a {1}, not a table".format(".".join(str(p) for p in parts[: index + 1]), child.kind)
                )
            parent = child
        return parent

    # -- rendering -----------------------------------------------------------

    @property
    def dirty(self):
        return self.root.dirty

    def render(self):
        """Re-emit the file.

        Untouched bytes come back verbatim; only dirty subtrees are rendered. A
        document with no changes therefore renders **byte-identically**.
        """
        if not self.root.dirty:
            return self.text
        head = self.text[: self.root.start]
        tail = self.text[self.root.end :]
        return head + self._render_table(self.root, 0) + tail

    def _render_value(self, node, indent):
        if isinstance(node, Scalar):
            return node.text
        if not node.dirty and not node.synthetic:
            return self.text[node.start : node.end]
        return self._render_table(node, indent)

    def _render_table(self, table, indent):
        if not table.dirty and not table.synthetic:
            return self.text[table.start : table.end]
        pad = "\t" * (indent + 1)
        close_pad = "\t" * indent
        out = ["{\n"]
        for entry in table.entries:
            out.append(pad)
            out.append(entry.key_text)
            out.append(" = ")
            out.append(self._render_value(entry.value, indent + 1))
            out.append(";\n")
        out.append(close_pad)
        out.append("}")
        return "".join(out)


def structural_diff(expected, actual, prefix="", out=None):
    """Compare two Python values and describe the differences.

    Used by the write path for validation-before-swap and for the post-write check
    (§15.2 steps 2 and 4), where the question is "is the file on disk what we meant
    to write", not "does it look nice".
    """
    if out is None:
        out = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            if key not in actual:
                out.append("{0}{1}: missing".format(prefix, key))
            else:
                structural_diff(expected[key], actual[key], "{0}{1}.".format(prefix, key), out)
        for key in actual:
            if key not in expected:
                out.append("{0}{1}: unexpected".format(prefix, key))
    elif expected != actual:
        out.append("{0}: expected {1!r}, found {2!r}".format(prefix.rstrip(".") or "<root>", expected, actual))
    return out


def iter_scalars(table, prefix=""):
    """Yield (path, Scalar) for every scalar in a table.

    Used by `profile show` and by the checks that count changed nodes.
    """
    for entry in table.entries:
        path = "{0}.{1}".format(prefix, entry.key) if prefix else str(entry.key)
        if isinstance(entry.value, Table):
            for item in iter_scalars(entry.value, path):
                yield item
        else:
            yield path, entry.value
