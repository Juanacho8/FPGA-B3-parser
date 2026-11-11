# Project documents

B3's specifications live in `spec/`, not here:

| File in `spec/`                                          | What                                      |
|----------------------------------------------------------|-------------------------------------------|
| `b3-market-data-messages-2.3.1.xml`                      | B3 schema (source of truth for layouts)   |
| `BinaryUMDF-MessageReference-v.2.3.1-enUS.pdf`           | Message Reference                         |
| `BinaryUMDF-MessageSpecificationGuidelines-v.2.3.1-enUS.pdf` | Specification Guidelines              |
| `LAYOUTS.md`                                             | Verified wire format and feed behavior    |

This folder holds project decisions (`PROJECT_CONTEXT.md`,
`FEED_GENERATOR_CONTEXT.md`). All protocol details (message layouts, template
IDs, field offsets, sequence-number rules) must come from `spec/`, never from
memory or outside sources. When code depends on the spec, cite the section
number in a comment or TODO.
