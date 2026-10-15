# Reference specs

Place exchange specifications here. All protocol details (message layouts,
template IDs, field offsets, sequence-number rules) must come from these
documents, never from memory or outside sources.

| File to add                  | Used by                                   |
|------------------------------|-------------------------------------------|
| B3 Binary UMDF spec (PDF)    | `rtl_parser/rtl/feed/b3/`, golden model, `cpp_baseline/`, `feed_generator/` |
| A5X market data spec (later) | future `rtl_parser/rtl/feed/a5x/`         |

When code depends on the spec, cite the section number in a comment or TODO.
