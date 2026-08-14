// Conventional Commits — see AGENTS.md's "Commit messages" section for the
// full type reference and why getting the type right matters: release-please
// reads these directly off main to compute the next semantic version and
// changelog. No local overrides needed — @commitlint/config-conventional's
// default type-enum (build, chore, ci, docs, feat, fix, perf, refactor,
// revert, style, test) already covers this repo's convention, and it does
// not restrict scope values, so scopes like "journal" in the automated
// chore(journal): paper reports commits need no extra allowlisting.
module.exports = {
  extends: ["@commitlint/config-conventional"],
};
