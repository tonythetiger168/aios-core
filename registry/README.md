# Public skill registry

Each folder here is one skill (`SKILL.md`, optionally `skill.toml` and reference files). On every
change to `main`, the `skill registry` workflow publishes them to
https://tonythetiger168.github.io/aios-core/, which is the default registry for
`aios skills search` and `aios skills install`.

To add a skill, open a pull request with a new folder. To change one, edit it **and bump its
version**: a published `name@version` never changes, and the build fails if its content would.
Earlier versions stay installable (`aios skills install name@1.0.0`).

Every skill must scan clean (`python aios.py skills scan registry/<name>`: no high or medium
findings). aios never runs anything in a skill; it only loads `SKILL.md` into the agent's prompt.
