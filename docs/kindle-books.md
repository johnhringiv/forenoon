# Books on the Kindle: what has gone wrong

A book that passes epubcheck can still break on the device. Amazon converts each emailed
EPUB, and the Kindle's renderer is closer to a browser parsing HTML than to an XML reader.
This page records what has gone wrong so far, what fixed it, and what is still unchecked.
Add to it whenever a book misbehaves.

## Problems found

### Self-closed empty elements: the Kindle rebooted (12 October 2026)

The Kindle kept rebooting on waking from the 12 October issue. The word limit below went in
first (20:20) and the reboots continued; they stopped only with this fix (21:24).

That issue held 56 empty elements written in XML's self-closed form: `<code/>`,
`<td/>`, `<a/>`, `<sup/>`, `<h2/>`, `<small/>`. Of these, 47 came from one chapter built from a GitHub diff page. That is valid
XHTML, but an HTML parser reads `<code/>` as an opening tag that never closes, so
everything after it becomes code.

Fix (159f0c6): `_no_self_closed` in `app/render.py` removes empty elements before a chapter
is written. A table's own parts, and anything carrying an id, stay with an explicit closing tag
(`<td></td>`). Only `br`, `hr` and `img` may be self-closed.

### A chapter far too long, suspected but not the cause (12 October 2026)

The same issue carried the whole NixOS manual as one chapter: 73,646 words, 688 KB, 715
code blocks, reached by following a link. It was suspected first for the reboots, but
holding it back did not stop them (see above). The limit stays, because a manual is not an
article.

Fix (144cc7b, 07d6cb4): an article over 45,000 words (`MAX_ARTICLE_WORDS` in `app/pipeline.py`) is
never delivered. This is a failsafe; the model choosing articles is the main control.

### Embedded fonts looked dropped, but were not (11 to 14 October 2026)

The golden test's code-font trial suggested that Send to Kindle drops embedded fonts. The
trial was wrong: it set the font on `<pre>`, and the base stylesheet's
`code { font-family: monospace }` matches the `<code>` inside it, which beats the
inherited value. Every sample showed the device font whatever the conversion did.

With the font set on `code` itself (14 October, Oasis):

- The embedded font is used with the reader's usual font setting. The Oasis has no
  "Publisher Font" option and does not need one.
- `font/ttf`, `application/x-font-ttf` and `application/vnd.ms-opentype` all work as the
  manifest type.
- `font-variant-ligatures` and `font-feature-settings` are ignored: a font's default
  ligatures (JetBrains Mono's `calt`) are always applied. To have no ligatures, embed a
  font without them (JetBrains Mono NL, IBM Plex Mono).

Rule: a font for code must be set on `code`, `kbd` and `samp` themselves, not only on `pre`.

### Code is not shown in a monospace font, and is justified and hyphenated (14 October 2026)

Found with a comparison book (IBM Plex Mono, JetBrains Mono NL, JetBrains Mono and the
device's own font, on code blocks taken from delivered articles):

- **`font-family: monospace` gets no monospace font.** The Oasis draws such code in its
  proportional reading font (Bookerly), so columns and ASCII art lose their alignment. Every
  book sent before this was found shows code this way. Only an embedded monospace font fixes
  it.
- **Code blocks are justified.** Lines that wrap are stretched to the full width, so the spaces
  between words in code grow (`z  :=  list.get_mut(2);  //  Wait..`). Inline code in a
  justified paragraph is stretched the same way.
- **Code is hyphenated.** The device breaks words inside `pre` and adds hyphens that are not
  in the source (`SSHFS ver-sion`, `<-oSer-verAliveInterval=15>`). Inline code breaks at its
  own hyphen (`out-` / `>ssid`).
- **JetBrains Mono's ligatures change what code appears to say.** `>=` and `<=` are drawn as
  `≥` and `≤`, `->` and `<-` as single arrows (`<-x>` reads `←x>`), and `==` as one long glyph.
  With no way to turn them off from CSS (above), use a font without ligatures.

What Amazon's [Kindle Publishing Guidelines](https://kindlegen.s3.amazonaws.com/AmazonKindlePublishingGuidelines.pdf)
(version 2026.3) say about this:

- Text is fully justified by default; set `text-align` on anything that should not be (11.3.1).
- `pre`, `code`, `samp`, `kbd` and `tt` are drawn in the Kindle's own monospaced font, and
  `pre` changes the alignment (11.3.8). The device did neither for our books, whose
  stylesheet names `font-family: monospace` and `white-space: pre-wrap` for `pre`.
- Enhanced Typesetting supports `hyphens: auto | manual` (not `none`) and
  `white-space: nowrap | normal` (not `pre-wrap`) (18.1).

Font test 4 (14 October, Oasis) showed:

- **Naming `monospace` is what loses the monospace font.** With no `font-family` on `pre` and
  `code`, the device draws them in its own monospaced font (a Courier-like slab). With
  `font-family: monospace`, as our stylesheet has, it draws them in Bookerly.
- **`text-align: left` with `hyphens: manual` on `pre` and `code` works:** no stretched
  spaces and no added hyphens. Lines still break after a hyphen that is in the source.
  Without these rules, even the device's own monospaced code is justified and hyphenated,
  and so is inline code in a paragraph (`No-` / `Payload`).
- **`white-space: pre` changes nothing:** long code lines wrap either way.
- **`white-space: nowrap` on inline code makes things worse:** the code moves whole to the
  next line and the justified line before it is stretched.
- The embedded font did not load on any page of this book, though the same file and
  `@font-face` loaded in test 3.

Font test 5 (14 October) loaded the embedded font on every page, including one with test 4's
exact rule. So the rule was not the cause; the one difference left is that test 4 also took
the stylesheet's own `font-family: monospace` lines out. It also showed that
**`hyphens: manual` does not stop hyphenation** (`SSHFS ver-sion`): in test 4 those lines
only escaped because Bookerly is narrower. `text-align: left` does stop the stretching.

Font test 6 (14 October): **`-webkit-hyphens: none` with `adobe-hyphenate: none` stops
hyphenation in code** (`SSHFS` / `version` break between words, nothing added).
`hyphens: manual`, `hyphens: none` and `lang="zxx"` each do not. Which of the two working
properties does it is not known; use both. Inline code in a justified paragraph still has
its spaces stretched with the rest of the line.

So the rules for code are (in `app/render.py` since 14 October):

```css
pre,
code,
kbd,
samp {
  font-family: "JetBrains Mono NL"; /* embedded; no fallback, so the device's own monospace is used if it fails */
  text-align: left;
  -webkit-hyphens: none;
  adobe-hyphenate: none;
}
```

One book contradicts the first finding: the Valen article built before 14 October, whose
stylesheet named `font-family: monospace` and embedded no font, was drawn in the device's own
Courier-like slab, not Bookerly. Every book that showed `monospace` as Bookerly embedded a
font. Untested guess: naming `monospace` loses the monospace font only in a book that embeds
one.

### Wrapped code lines start at the left edge (14 October 2026)

A code line too long for the screen wraps, and the wrapped part starts at the left edge of the
block, where it reads as a new line of code; indentation stops showing structure. Found with
the Valen article's Rust (`let entity_mut = &mut world.entities[entity_id]; // bounds check,
possible panic`), and tested with six books on the Oasis, measured in screenshots.

- **A hanging indent works, in em only.** Each source line as a block
  (`span { display: block }`) with `padding-left: Xem; text-indent: -Xem` puts wrapped parts
  X in. The same in `ch` does nothing: the guidelines list no `ch` for either property.
- **An em in `padding-left` and `text-indent` is a fixed size, whatever the reader's font
  size.** At two sizes the same rules put text at the same pixel (26 px per em), while a code
  character went from 23 to 26 px. So nothing in CSS scales with the font setting; only text
  does. (`font-size` in em does apply: code is smaller than body text.)
- **Spaces at the start of a block are drawn 9 px wide, whatever the font size** (a code
  character is 23 to 26 px). Non-breaking spaces too. Spaces later in a line, and at the start
  of the second and later lines of a plain `pre`, are a full character. So a 2-space indent in
  a line block showed as one character, and an 8-space one as three.
- **A token longer than the room left is split with a hyphen** (`get_collision_for_entity(en-`
  / `tity_read);`), whatever the CSS: `-webkit-hyphens: none` and `adobe-hyphenate: none`
  (which do stop hyphenation between words), `hyphens: manual`, `word-wrap: normal` and
  `overflow-wrap: normal` all leave it. `word-break: break-all` stops it, but then every line
  breaks at the last character that fits, inside identifiers (`usi` / `ze`).
- **`<wbr>` is ignored, and so is a zero-width non-joiner (U+200C)** as a break point. **A
  zero-width space (U+200B) works:** the line breaks there, with nothing added. The guidelines
  say only space, nbsp and zwnj are supported, and format characters "may cause problems"; the
  book with them opened and read fine.
- **Leading spaces kept in the text of a line block never come out at character width**, so
  they cannot carry a code line's indent at every font size. Tried after a zero-width space,
  after a zero-width non-joiner, and inside an inline `span`, each with the hang in CSS: the
  spaces were wider than a character (or narrower, in the `span`) and each indent level added
  more error, so a tree fanned out. The indent in em, fixed pixels, was the only version that
  lined up at both sizes tried. Do not try this again on the Oasis; see below for the numbers.
- The font setting offers "Publisher Font" for a book whose stylesheet sets a font on `pre,
code` for the whole book, not for one that sets it through a class. It changes nothing.

So code is built (`_code_lines` in `app/render.py`) as one `span.ln` per source line, with the
line's indent taken out of the text and set in em with the hang (`.iN`: `padding-left:
(N + 4) × 0.9em; text-indent: -3.6em`), and a zero-width space after `. , ; : ( [ { / ? = & < >`.
One indent character (0.9 em) measured 25 px on the Oasis, between a code character at the
usual size (23 px) and at the next larger one (26 px), so tree output and box diagrams line up
at both: a 4-space step is 4.35 characters at the usual size and 3.85 at the larger. Keeping
the spaces in the text instead does not work: after a zero-width space or non-joiner, or inside
an inline `span`, leading spaces come out wider than a character and each level wider than the
last (4, 8, 12, 16 spaces measured 123, 265, 425, 606 px at the usual size; inside a `span`,
61, 132, 216, 313 px at both sizes). The CSS indent was the only one of the four that kept a
tree's branches aligned.
Highlighting elements that span lines are closed and reopened, tabs become four spaces, and an
empty line keeps its height with a non-breaking space.

## Rules that came from the device

- Images are flattened onto white before conversion, so transparent diagrams stay readable.
- The cover is in colour only when images are (the reader's "My Kindle has a colour screen",
  on their Account page). A grayscale
  cover carries the ink-only version of the mark (`app/mark.py`), whose hatching and solid
  black need no dithering on e-ink; the colour version's tints would turn into grey speckle.
- The Kindle's browser needs TLS 1.2, so the public hostname's zone must not require 1.3.
- Send to Kindle takes EPUB by email up to the relay's size limit (see
  [setup-email.md](setup-email.md)).

### Emoji do not render, unless set in an embedded font (14 October 2026)

The 14 October issue's "nobody should type code by hand anymore" has 💩 (U+1F4A9) in a
sentence; on the Oasis it does not show, because the reading fonts have no emoji. They are
rare: 3 of 119 saved articles have any (🔗 ×14, mostly heading-link icons, 💩 ×2, 🎉 ×1).

Each emoji is now wrapped in a span set in Noto Emoji (black and white, OFL), cut down to the
emoji the book uses (17 KB for 18 emoji). A test book showed on the Oasis (`app/emoji.py`):

- Every emoji renders, in body text, titles, headings, lists and code.
- Sequences render as one glyph: families and professions joined with U+200D, the rainbow
  flag, heart on fire, country flags (drawn as a lettered flag outline) and keycaps. Skin
  tones show the plain emoji, since the font has no colour.
- The font must be a fixed weight: Noto Emoji is published as a variable font, so the build
  makes the Regular instance (the Kindle's support for variable fonts is not known).
- Symbols with a text form of their own (`© ™ ❤ → ★` without U+FE0F) are left to the
  reading font, which has them.

### Math symbols render in the reading font (21 October 2026)

Formulas reach the book as text (`app/formulas.py`), so their symbols must be in a font the
device has. A test book (Oasis, Bookerly) showed each symbol the saved articles' formulas
use, in the reading font beside an embedded Noto Sans Math:

- **Every one renders in the reading font**, with no box or blank:
  `− × · ± ∑ ∏ ∫ √ ∞ ∂ ∇ ∈ ∉ ⊂ ⊆ ∪ ∩ ≈ ≠ ≡ ≤ ≥ ≪ ∼ ∝ ⊤ ⊥ ⊙ ⊕ ‖ ′`,
  `ℓ ℝ ℕ ℤ → ← ↔ ⇒ ⇔ ↦ … ⋯ ⟨ ⟩ ⌊ ⌋ ∀ ∃ ∅ ϵ ϕ`, and Greek, upright and italic. Arrows are drawn small, and `⌊ ⌋` look like `L` and `⅃`. So no
  math font is embedded.
- **Combining accents work**, and look better in the reading font than in Noto Sans Math:
  `θ̂ x̂ x̃ ẋ ẍ`, and an overline (U+0305) over one letter or over each of several (`A̅B̅`). In
  Noto Sans Math the overline lands beside the letter.
- `<sub>` and `<sup>` render, nested too (`2` with `k−52` raised). A subscript and a
  superscript on one letter sit one after the other, not stacked (`∑` then `i=1` then `M`).
- A formula kept as TeX in `<code>` alone on its line is justified like the paragraph it is
  in, so its spaces stretch (see code above). Such a formula is put in `<pre>`.
- Not yet seen on the device, from the same blocks as the symbols above: `ℂ ℚ ⋆ ∖ ∬ ∓ ≜ ⌈ ⌉ ≔`,
  and `⩽ ⩾` from arXiv's MathML. KaTeX's `∣ ∥ ∗ ⟺` are written as `| ‖ * ⇔`, which were seen.

## Not yet checked on the device

Found by scanning the three issues sent so far (12 to 14 October):

- **Box drawing and arrows in code** (`├─ │ └`, `→ ← ↑ ↓`, from `tree` output and diagrams).
  JetBrains Mono NL has all of those seen so far except `⇄`. The golden test book has a
  `tree`-style block to check them.
- **Very long lines in code** (up to 255 characters). An 86-character line wrapped (above);
  longer ones should too.
- Deep nesting (up to 21 levels in one chapter) has caused no trouble seen so far.

## How to check a book on the device

- `forenoon golden` writes the golden rendering-test EPUB: headings, code, tables, lists,
  images (including transparent ones), footnotes and rating links.
- **Screenshots on the Oasis:** tap two opposite corners of the screen at once (top-left
  and bottom-right); the screen flashes. A PNG is saved in the top-level folder you see over
  USB, named like `screenshot_2026_10_14T16_46_56-0400.png`.
- Copies of every book sent are kept in `<data>/issues/`, so a book that misbehaved can be
  examined afterwards.
