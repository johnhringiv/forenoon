# Forenoon

## Books for the Kindle

Before changing how books are built (`app/render.py`, the stylesheet, fonts, images), read
[docs/kindle-books.md](docs/kindle-books.md): what has broken on the device and the rules that
came from it. A book that passes epubcheck can still break on the Kindle.

Amazon's reference for what the Kindle supports is the
[Kindle Publishing Guidelines](https://kindlegen.s3.amazonaws.com/AmazonKindlePublishingGuidelines.pdf)
(version 2026.3 checked on 14 October 2026). Check a CSS property's supported values there
before relying on it: Enhanced Typesetting accepts only some values, for example
`hyphens: auto | manual` (not `none`) and `white-space: nowrap | normal` (not `pre-wrap`).
