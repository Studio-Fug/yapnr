"""Code keys of pnr.feedback.signals survive formatting and still read legacy records.

Scheme 2 (the default) hashes each evaluation module's canonical syntax tree by
module name, so black and isort output keys like its input and any change to
what the code does changes the key. Records without ``code_key_scheme`` carry a
legacy (scheme 1, raw file bytes) key and are compared under that scheme, or
re-keyed from their evaluation tree while it still hashes to their stamp.
Stdlib only; the black/isort round trip runs where both are importable.
"""

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pnr.feedback import signals

try:
    import black
    import isort
except ImportError:  # not in the Bazel test environment; the hand-made pairs below cover it
    black = isort = None

# Splanc-style source and the same code as black (100 columns) and isort (black profile) write it.
BEFORE = '''\
"""Module docstring.
   Indented continuation line.
"""
from pnr.shove.world import line_length_limit as lim, clamp
import sys, os
from pnr.shove import world
from pnr.shove.world import clamp  # duplicate, isort drops it
X = {'a': 1, 'b': [1,2,3]}; Y = u'text'
def f(a, b = 2, *args, **kw):
        """Doc of f.

        Returns a tuple.
        """
        import json
        from collections import OrderedDict, defaultdict
        del (a, b)
        return (json.dumps(X), OrderedDict(), defaultdict(list), lim(1), os.sep, sys.argv)  # trailing comment
class C(object):
    def g(self): return [x for x in range(10) if x%2==0]
'''
AFTER = '''\
"""Module docstring.
Indented continuation line.
"""

import os
import sys

from pnr.shove import world
from pnr.shove.world import clamp  # duplicate, isort drops it
from pnr.shove.world import line_length_limit as lim

X = {"a": 1, "b": [1, 2, 3]}
Y = "text"


def f(a, b=2, *args, **kw):
    """Doc of f.

    Returns a tuple.
    """
    import json
    from collections import OrderedDict, defaultdict

    del (a, b)
    return (
        json.dumps(X),
        OrderedDict(),
        defaultdict(list),
        lim(1),
        os.sep,
        sys.argv,
    )  # trailing comment


class C(object):
    def g(self):
        return [x for x in range(10) if x % 2 == 0]
'''

# Each changes what the code does (or says), so each must change the key.
SEMANTIC = [
    ("X = {'a': 1", "X = {'a': 2"),  # a constant
    ("'b': [1,2,3]", "'b': [1.0,2,3]"),  # int -> float
    ("'b': [1,2,3]", "'b': [True,2,3]"),  # int -> bool
    ("x%2==0", "x%2!=0"),  # an operator
    ("b = 2", "b = 3"),  # a default
    ("def f(a, b", "def f(b, a"),  # argument order
    ("Returns a tuple.", "Returns a list."),  # docstring content
    ("lim(1), os.sep", "os.sep, lim(1)"),  # expression order
    ("import sys, os", "import sys, os as o"),  # an import binding
    ("from pnr.shove import world", "from pnr.shove import ladder"),
    ("del (a, b)", "del (a,)"),  # a del target
    ("class C(object):", "class C:"),  # a base class
    ("Y = u'text'", "Y = b'text'"),  # str -> bytes
]


def canonical(src):
    return signals.canonical_source(ast.parse(src))


def formatted(src):
    return black.format_str(
        isort.code(src, profile="black", line_length=100), mode=black.Mode(line_length=100)
    )


class CanonicalTest(unittest.TestCase):
    def test_formatting_keeps_the_canonical_form(self):
        self.assertNotEqual(ast.dump(ast.parse(BEFORE)), ast.dump(ast.parse(AFTER)))
        self.assertEqual(canonical(BEFORE), canonical(AFTER))

    @unittest.skipUnless(black and isort, "black and isort not importable")
    def test_black_and_isort_output(self):
        self.assertEqual(formatted(BEFORE), AFTER)  # AFTER is what the tools write
        for f in sorted(signals.PNR_ROOT.glob("pnr/**/*.py")):
            src = f.read_text()
            self.assertEqual(canonical(src), canonical(formatted(src)), f)

    def test_semantic_changes_change_it(self):
        base = canonical(BEFORE)
        for old, new in SEMANTIC:
            self.assertIn(old, BEFORE)
            self.assertNotEqual(canonical(BEFORE.replace(old, new, 1)), base, new)

    def test_imports_sort_only_where_order_cannot_matter(self):
        same = (
            "import os\nimport sys\n",
            "import sys, os\n",
            "import sys\nimport os\nimport sys\n",
        )
        self.assertEqual(len({canonical(s) for s in same}), 1)
        self.assertEqual(
            canonical("from a import x, y\n"), canonical("from a import y\nfrom a import x\n")
        )
        self.assertEqual(canonical("import a.b\nimport a\n"), canonical("import a\nimport a.b\n"))
        # one name bound twice (the later binding wins), star imports: the order is kept
        self.assertNotEqual(
            canonical("from a import f\nfrom b import f\n"),
            canonical("from b import f\nfrom a import f\n"),
        )
        self.assertNotEqual(
            canonical("from a import *\nfrom b import *\n"),
            canonical("from b import *\nfrom a import *\n"),
        )
        # an import never moves across other code, nor into another block
        self.assertNotEqual(
            canonical("import a\nx = 1\nimport b\n"), canonical("import b\nx = 1\nimport a\n")
        )
        self.assertNotEqual(
            canonical("import a\ndef f():\n    import b\n"),
            canonical("import b\ndef f():\n    import a\n"),
        )
        self.assertNotEqual(canonical("from . import a\n"), canonical("from .. import a\n"))

    def test_what_black_changes_in_a_tree(self):
        # black's own AST check allows these (it drops the parentheses and the u prefix,
        # re-indents docstrings); comments are not in the tree at all
        self.assertEqual(canonical("del (a, b)\n"), canonical("del a, b\n"))
        self.assertEqual(canonical("x = u'a'\n"), canonical('x = "a"\n'))
        self.assertEqual(
            canonical('def f():\n    """A\n       b.  \n    """\n'),
            canonical('def f():\n    """A\n    b.\n    """  # c\n'),
        )
        # a string that is not a whole statement keeps its whitespace
        self.assertNotEqual(canonical("x = 'a  '\n"), canonical("x = 'a'\n"))

    def test_empty_fields_are_left_out(self):
        # fields that some Python versions add empty (3.12's type_params) do not enter the form
        text = canonical("def f():\n    return x\n")
        for field in ("type_params", "decorator_list", "type_ignores", "kind", "returns"):
            self.assertNotIn(field + "=", text)
        self.assertIn("str:'f'", text)

    def test_deep_trees(self):
        # ast.dump recurses and fails on this tree; parsing it works on Python 3.9-3.12
        self.assertTrue(canonical("x = " + " + ".join(["1"] * 2000) + "\n"))


MINI = {
    "pnr/__init__.py": "",
    "pnr/full_iteration.py": "import os\nfrom pnr.native_loop import run\nCMD = ['-m', 'pnr.shove']\n",
    "pnr/native_loop.py": (
        "def run():\n    if os.environ.get('PNR_SHOVE') == '1':\n"
        "        from pnr.shove.world import w\n"
    ),
    "pnr/shove/__init__.py": "",
    "pnr/shove/world.py": "def w(n):\n    return min(.5, max(.05 * n, .15))\n",
    "pnr/hier/__init__.py": "",
    "pnr/hier/native_block.py": "from pnr.hier import blocks\n",
    "pnr/hier/blocks.py": "",
    "pnr/hier/synth.py": "",
}
WORLD_BLACK = "def w(n):\n    return min(0.5, max(0.05 * n, 0.15))  # formatted\n"
WORLD_FROZEN = "def w(n):\n    return min(.5, .05 * n)\n"  # src10.frozen: another router


def make_tree(root, files=MINI):
    for rel, text in files.items():
        f = Path(root) / "hardware" / "pnr" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return Path(root) / "hardware" / "pnr"


def make_round(d, tree):
    """A round whose native loop recorded ``tree`` as its annotation source."""
    o = Path(d) / "electrical" / "native-loop" / "source-inputs"
    o.mkdir(parents=True)
    ato = str(Path(tree) / "hardware/splanc_dev/elec/src/splanc_mini.ato")
    (o / "origins.json").write_text(json.dumps([dict(original=ato, snapshot="s", sha256="x")]))
    return Path(d)


class SchemeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.root = make_tree(self.d / "run")

    def tearDown(self):
        self.tmp.cleanup()

    def edit_world(self, text, root=None):
        (Path(root or self.root) / "pnr/shove/world.py").write_text(text)

    def test_new_key_ignores_formatting_only(self):
        files = signals.eval_code_files(self.root, "shove")
        self.assertEqual(
            set(files),
            {
                "pnr",
                "pnr.full_iteration",
                "pnr.native_loop",
                "pnr.shove",
                "pnr.shove.world",
                "pnr.hier",
                "pnr.hier.native_block",
                "pnr.hier.blocks",
                "pnr.hier.synth",
            },
        )
        before = signals.code_key(self.root, "shove"), signals.code_key(
            self.root, "shove", scheme=1
        )
        self.edit_world(WORLD_BLACK)
        self.assertEqual(signals.code_key(self.root, "shove"), before[0])
        self.assertNotEqual(signals.code_key(self.root, "shove", scheme=1), before[1])
        self.edit_world(WORLD_FROZEN)
        self.assertNotEqual(signals.code_key(self.root, "shove"), before[0])
        self.assertEqual(
            signals.code_diff(files, signals.eval_code_files(self.root, "shove")),
            ["pnr.shove.world"],
        )

    def test_legacy_scheme_is_the_raw_bytes_key(self):
        rels = [
            "pnr/__init__.py",
            "pnr/full_iteration.py",
            "pnr/native_loop.py",
            "pnr/hier/__init__.py",
            "pnr/hier/native_block.py",
            "pnr/hier/blocks.py",
            "pnr/hier/synth.py",
        ]
        raw = {r: hashlib.sha1((self.root / r).read_bytes()).hexdigest() for r in rels}
        want = hashlib.sha1(json.dumps(sorted(raw.items())).encode()).hexdigest()[:10]
        self.assertEqual(
            signals.eval_code_files(self.root, "plain", scheme=1), dict(sorted(raw.items()))
        )
        self.assertEqual(signals.code_key(self.root, "plain", scheme=1), want)
        self.assertEqual(signals.TreeCode(self.root, "plain").key(1), want)
        with self.assertRaises(ValueError):
            signals.eval_code_files(self.root, "plain", scheme=3)

    def test_stamp_and_record_scheme(self):
        stamp = signals.code_stamp(self.root, "shove")
        self.assertEqual(stamp, dict(code=signals.code_key(self.root, "shove"), code_key_scheme=2))
        self.assertEqual(signals.code_key_scheme(stamp), signals.CODE_KEY_SCHEME)
        self.assertEqual(signals.code_key_scheme(dict(code="abc")), signals.LEGACY_CODE_KEY_SCHEME)
        self.assertEqual(signals.code_key_scheme(None), signals.LEGACY_CODE_KEY_SCHEME)

    def check(self, record, round_dir=None, cache=None):
        obs = signals.observed_code(
            round_dir,
            "shove",
            stamped=record.get("code"),
            cache=cache,
            scheme=record.get("code_key_scheme"),
        )
        return obs, signals.check_code(obs, signals.TreeCode(self.root, "shove"))

    def test_new_records_survive_a_reformat(self):
        rec = signals.code_stamp(self.root, "shove")
        self.assertEqual(self.check(rec)[1], ([], [], False))
        self.edit_world(WORLD_BLACK)
        self.assertEqual(self.check(rec)[1], ([], [], False))
        self.edit_world(WORLD_FROZEN)
        errors, _, _ = self.check(rec)[1]
        self.assertEqual(len(errors), 1)
        self.assertNotIn("scheme", errors[0])

    def test_legacy_records_are_compared_under_the_legacy_scheme(self):
        rec = dict(code=signals.code_key(self.root, "shove", scheme=1))  # no code_key_scheme
        obs, result = self.check(rec)
        self.assertEqual((obs["code_key_scheme"], obs["reason"]), (1, "stamped"))
        self.assertEqual(result, ([], [], False))  # unchanged code: accepted
        self.edit_world(WORLD_BLACK)  # this run's tree reformatted
        obs, (errors, _, _) = self.check(rec)
        self.assertEqual(len(errors), 1)
        self.assertIn("(code key scheme 1)", errors[0])
        errors, warnings, stale = signals.check_code(
            obs, signals.TreeCode(self.root, "shove"), policy="rebase"
        )
        self.assertEqual((errors, len(warnings), stale), ([], 1, True))

    def test_legacy_records_are_re_keyed_from_their_unchanged_tree(self):
        frozen = make_tree(self.d / "src10")  # the tree that ran them
        rnd = make_round(self.d / "round", self.d / "src10")
        rec = dict(code=signals.code_key(frozen, "shove", scheme=1))
        self.edit_world(WORLD_BLACK)  # this run: reformatted
        cache = {}
        obs, result = self.check(rec, rnd, cache)
        self.assertEqual(result, ([], [], False))
        self.assertEqual((obs["code_key_scheme"], obs["tree"]), (2, str(self.d / "src10")))
        self.assertIn("re-keyed", obs["reason"])
        self.assertEqual(obs["code"], signals.code_key(frozen, "shove"))
        self.assertIn((str(frozen), "shove"), cache)
        # the evaluation tree changed since: its bytes no longer prove what ran, so the stamp stays legacy
        self.edit_world(WORLD_BLACK, frozen)
        obs, (errors, _, _) = self.check(rec, rnd)
        self.assertEqual((obs["code_key_scheme"], obs["reason"]), (1, "stamped"))
        self.assertEqual(len(errors), 1)
        # a different evaluation code is refused either way
        self.edit_world(WORLD_FROZEN, frozen)
        rec = dict(code=signals.code_key(frozen, "shove", scheme=1))
        obs, (errors, _, _) = self.check(rec, rnd)
        self.assertEqual(obs["code_key_scheme"], 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("differs: pnr.shove.world", errors[0])

    def test_unknown_scheme_is_refused(self):
        rec = dict(code=signals.code_key(self.root, "shove"), code_key_scheme=3)
        obs, (errors, _, _) = self.check(rec)
        self.assertEqual(obs["code_key_scheme"], 3)
        self.assertIn("unknown to this code", errors[0])

    def test_tree_lookup_uses_the_new_scheme(self):
        rnd = make_round(self.d / "round", self.d / "run")
        obs = signals.observed_code(rnd, "shove")
        self.assertEqual(
            (obs["code"], obs["code_key_scheme"], obs["reason"]),
            (signals.code_key(self.root, "shove"), 2, "tree"),
        )

    def test_real_tree(self):
        for router in ("plain", "shove"):
            code = signals.TreeCode(signals.PNR_ROOT, router)
            self.assertEqual(code.key(), signals.code_key(signals.PNR_ROOT, router))
            self.assertIn("pnr.full_iteration", code.files())
            self.assertIn("pnr/full_iteration.py", code.files(1))
            self.assertEqual(len(code.files()), len(code.files(1)))
            self.assertFalse(
                any(v.startswith("bytes:") for v in code.files().values())
            )  # every module parses
        k = signals.current_key("block", 600)
        self.assertEqual(
            (k["code"], k["code_key_scheme"]), (signals.code_key(signals.PNR_ROOT, k["router"]), 2)
        )


if __name__ == "__main__":
    unittest.main()
