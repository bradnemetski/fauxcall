---
name: extend
description: Add endpoints or MCP tools to an existing FauxCall fake — newly published by the real API or previously out of scope — with the same record, model, consistency and parity rigor as the original build. Use when the user runs /fauxcall:extend <name> [endpoints…], or asks to add endpoints or tools to a fake.
license: Apache-2.0
compatibility: Needs uv, a shell and network access to the real service.
metadata:
  author: Brad Nemetski
allowed-tools: Bash(fauxcall *) Bash(uvx fauxcall *)
---

# Extend a fake

Arguments: the fake's name, then optional endpoint ids/globs or tool names (default: everything in
the current spec that the model lacks). Follow the main `/fauxcall` skill's rules and guides.

0. `fauxcall doctor --skill-dir "<the folder this SKILL.md is in>"`: if it says the skill doesn't
   match the CLI, stop and give the user the fix it prints.
1. Find what's missing: REST — re-import the current spec (`fauxcall spec import <name> <file>` after
   saving the latest spec under `data/source/`), compare with `fauxcall endpoints <name>`;
   MCP — `fauxcall mcp introspect <name>` (keeps modeled behavior of existing tools).
2. Mini-gate: new credentials? new mutating calls? Plan them (`data/plans/`), `fauxcall plan review`,
   ask the user once, `fauxcall plan approve … --planned`.
3. Run the endpoint loop (record → analyze → model → checkpoint) for each new endpoint, in dependency
   order; update collections/relations; `fauxcall world build <name>`.
4. `fauxcall scaffold <name>` (adds routes), implement handlers, add unit and e2e scenarios for the
   new endpoints **and** for their interactions with existing ones.
5. `fauxcall verify <name>` and `--target parity` until ≥ 98 % overall; `fauxcall report <name>`.
