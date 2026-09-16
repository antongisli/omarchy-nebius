# Contributing

This is the independent Nebius GPU plugin for Omarchy. Changes must preserve
unrelated user configuration, integrations, and cloud resources.

Treat every tracked file and commit message as public. Do not include private
conversations, unpublished product claims, account identifiers, personal
filesystem paths, or credentials. Product-status claims must be supported by
public documentation or reproducible behavior from released public tools.

## Safety boundaries

- Tests must use temporary homes and synthetic cloud responses, never a
  contributor's account, credentials, desktop session, or billable resources.
- Installation and removal behavior is part of the public interface. Keep the
  documented confirmation, ownership, and cloud-resource boundaries intact.
- Do not add `AGENTS.md` anywhere in the repository. Omarchy installs the whole
  source tree, and agent instruction files must not ship as trusted workspace
  instructions. CI rejects them.

## Development checks

Use Python 3.11+ and run `python3 -m unittest discover -s tests -v`. Run
`bash -n` on changed shell scripts. See `docs/development.md` for Linux
integration checks against unmodified Omarchy.
