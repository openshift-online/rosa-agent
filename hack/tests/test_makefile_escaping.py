"""Guard against bare $N in Makefile recipe lines.

In Make, recipe lines are passed to the shell after Make variable expansion.
Make expands $N (digit) as the empty variable "N" — not as a shell positional
parameter. The shell therefore never sees $1, $2, etc. from awk, cut, or
similar tools; it sees an empty string, silently changing behaviour.

The fix is always $$N in a recipe: Make emits a literal $ to the shell.

  Bug:  awk 'NR>1{print $1}'    # Make expands $1 → shell sees: print
  Fix:  awk 'NR>1{print $$1}'   # Make emits $$1 → shell sees: $1

This test catches the class of bug documented at:
  https://www.gnu.org/software/make/manual/make.html#Rule-Recipe
"""

import re
import unittest
from pathlib import Path

MAKEFILE = Path(__file__).parent.parent.parent / "Makefile"

# Match $N (digit) NOT preceded by another $ — i.e. a bare (un-escaped) dollar-digit.
# $$1 (correct) is NOT matched because the lookbehind sees the preceding $.
_BARE_DOLLAR_DIGIT = re.compile(r'(?<!\$)\$[0-9]')


class TestMakefileEscaping(unittest.TestCase):
    """No Makefile recipe line may contain a bare $N (digit) variable reference."""

    def test_no_bare_dollar_digit_in_recipes(self):
        lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
        violations = []
        for i, line in enumerate(lines, 1):
            if not line.startswith("\t"):
                continue  # only recipe lines (tab-indented) are shell-expanded
            if _BARE_DOLLAR_DIGIT.search(line):
                violations.append(f"  line {i}: {line!r}")
        self.assertFalse(
            violations,
            "Bare $N in Make recipe (Make expands these as empty variables; use $$N):\n"
            + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
