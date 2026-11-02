"""Design checks for site/css/styles.css: contrast in both themes, and no value that bypasses the tokens.

Run from the repository root:
    python3 -I -m unittest discover --start-directory tests --top-level-directory .

Contrast follows WCAG 2.2 level AA: 4.5:1 for text, 3:1 for the edges of controls (1.4.11).
Exit code 0 means every check passed.
"""
import re
import unittest
from pathlib import Path
from typing import NamedTuple

import build

STYLES_PATH = Path(__file__).resolve().parents[1] / "site" / "css" / "styles.css"
TEXT_CONTRAST = 4.5
CONTROL_EDGE_CONTRAST = 3.0
THEMES = ("light", "dark")


class ContrastPair(NamedTuple):
    foreground: str
    background: str
    minimum: float
    where: str


STATES = tuple(state.value for state in build.State)
CONTRAST_PAIRS = (
    ContrastPair("text", "background", TEXT_CONTRAST, "body text"),
    ContrastPair("text", "surface", TEXT_CONTRAST, "text on cards and controls"),
    ContrastPair("text-muted", "background", TEXT_CONTRAST, "descriptions, labels, footer, unsorted arrow"),
    ContrastPair("text-muted", "surface", TEXT_CONTRAST, "placeholder, card replacement"),
    ContrastPair("accent", "background", TEXT_CONTRAST, "links"),
    ContrastPair("accent", "surface", TEXT_CONTRAST, "links on cards"),
    ContrastPair("link-hover", "background", TEXT_CONTRAST, "hovered links"),
    ContrastPair("link-hover", "surface", TEXT_CONTRAST, "hovered links on cards"),
    ContrastPair("background", "text", TEXT_CONTRAST, "pressed filter and theme buttons"),
    ContrastPair("state-end-date-set-text", "background", TEXT_CONTRAST, "days left in the table"),
    ContrastPair("state-end-date-set-text", "surface", TEXT_CONTRAST, "dates on cards"),
    *(ContrastPair(foreground, "state-end-date-set-fill", TEXT_CONTRAST, "a row opened from a card")
      for foreground in ("text", "text-muted", "accent", "link-hover")),
    *(ContrastPair(foreground, "row-hover", TEXT_CONTRAST, "a row under the pointer")
      for foreground in ("text", "text-muted", "accent", "link-hover", "state-end-date-set-text")),
    ContrastPair("control-border", "surface", CONTROL_EDGE_CONTRAST, "edges of inputs and buttons"),
    ContrastPair("control-border", "background", CONTROL_EDGE_CONTRAST, "edges of controls on the page"),
    *(ContrastPair(f"state-{state}-text", f"state-{state}-fill", TEXT_CONTRAST, f"{state} badge") for state in STATES),
)

# Declarations outside :root may only read tokens for these properties.
SPACING_PROPERTY = re.compile(r"^(?:margin|padding)(?:-(?:top|right|bottom|left))?$|^(?:row-|column-)?gap$")
SPACING_VALUE = re.compile(r"^(?:0|auto|var\(--space-[a-z0-9-]+\))$")
TOKEN_ONLY_PROPERTIES = {
    "font-size": re.compile(r"^var\(--text-[a-z0-9-]+\)$"),
    "font-weight": re.compile(r"^var\(--weight-[a-z]+\)$"),
    "border-radius": re.compile(r"^(?:0|var\(--radius-[a-z]+\))$"),
}
RAW_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|oklch)\(")
COLOR_TOKEN = re.compile(r"--color-([a-z0-9-]+):\s*light-dark\(\s*(#[0-9a-fA-F]{6})\s*,\s*(#[0-9a-fA-F]{6})\s*\)")


def read_styles() -> str:
    return re.sub(r"/\*.*?\*/", "", STYLES_PATH.read_text(encoding="utf-8"), flags=re.DOTALL)


def color_tokens(styles: str) -> dict[str, dict[str, str]]:
    """Token name -> {"light": hex, "dark": hex}, from the light-dark() pairs."""
    return {name: {"light": light, "dark": dark} for name, light, dark in COLOR_TOKEN.findall(styles)}


def relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def find_contrast_failures(styles: str) -> list[str]:
    tokens, failures = color_tokens(styles), []
    for pair in CONTRAST_PAIRS:
        for theme in THEMES:
            ratio = contrast_ratio(tokens[pair.foreground][theme], tokens[pair.background][theme])
            if ratio < pair.minimum:
                failures.append(f"{theme} {pair.where}: {pair.foreground} on {pair.background}"
                                f" is {ratio:.2f}, needs {pair.minimum}")
    return failures


def declarations_outside_tokens(styles: str) -> list[tuple[str, str]]:
    """(property, value) of every declaration except custom properties, which are the tokens."""
    found = []
    for block in re.findall(r"\{([^{}]*)\}", styles):
        for declaration in block.split(";"):
            if ":" not in declaration:
                continue
            name, value = (part.strip() for part in declaration.split(":", 1))
            if not name.startswith("--"):
                found.append((name, value.replace("!important", "").strip()))
    return found


def find_missing_badge_rules(styles: str) -> list[str]:
    """Every state build.py can give a row needs its own badge rule, or the badge shows no colors."""
    return [state for state in STATES if not re.search(rf"\.state-badge\.{re.escape(state)}\s*\{{", styles)]


def find_token_bypasses(styles: str) -> list[str]:
    bypasses = []
    for name, value in declarations_outside_tokens(styles):
        if RAW_COLOR.search(value):
            bypasses.append(f"{name}: {value} uses a raw color")
        if SPACING_PROPERTY.match(name) and not all(SPACING_VALUE.match(part) for part in value.split()):
            bypasses.append(f"{name}: {value} is not on the space scale")
        rule = TOKEN_ONLY_PROPERTIES.get(name)
        if rule and value != "inherit" and not rule.match(value):
            bypasses.append(f"{name}: {value} does not read a token")
    return bypasses


class DesignTest(unittest.TestCase):
    def setUp(self) -> None:
        self.styles = read_styles()

    def test_every_pair_has_both_themes(self) -> None:
        tokens = color_tokens(self.styles)
        names = {name for pair in CONTRAST_PAIRS for name in (pair.foreground, pair.background)}
        self.assertEqual(sorted(names - tokens.keys()), [])

    def test_contrast_meets_wcag_aa_in_both_themes(self) -> None:
        self.assertEqual(find_contrast_failures(self.styles), [])

    def test_rules_read_only_tokens(self) -> None:
        self.assertEqual(find_token_bypasses(self.styles), [])

    def test_every_state_has_a_badge_rule(self) -> None:
        self.assertEqual(find_missing_badge_rules(self.styles), [])

    def test_planted_missing_badge_rule_is_caught(self) -> None:
        planted = self.styles.replace(".state-badge.renamed {", ".state-badge.renamed-old {")
        self.assertNotEqual(planted, self.styles)
        self.assertEqual(find_missing_badge_rules(planted), ["renamed"])

    def test_planted_pale_color_is_caught(self) -> None:
        pale = self.styles.replace("--color-text-muted: light-dark(#5c576b,", "--color-text-muted: light-dark(#b0abb9,")
        self.assertNotEqual(pale, self.styles)
        self.assertTrue(any("text-muted" in failure for failure in find_contrast_failures(pale)))

    def test_planted_raw_values_are_caught(self) -> None:
        planted = self.styles + ".planted { margin: 13px; font-size: 15px; color: #123456; }"
        self.assertEqual(len(find_token_bypasses(planted)), 3)


if __name__ == "__main__":
    unittest.main()
