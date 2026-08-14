// Conventional Commits — see AGENTS.md's "Commit messages" section for the
// full type reference and why getting the type right matters: release-please
// reads these directly off main to compute the next semantic version and
// changelog. type-enum and scope are left at @commitlint/config-conventional's
// defaults (type-enum: build, chore, ci, docs, feat, fix, perf, refactor,
// revert, style, test; scope is unrestricted, so "journal" in the automated
// chore(journal): paper reports commits needs no extra allowlisting).
module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    // Dependabot's own commit/PR titles ("Bump X from Y to Z") always
    // capitalize the first word after the type/scope prefix, regardless of
    // dependabot.yml's commit-message config (verified against
    // dependabot-core's title_builder.rb: capitalize? is unconditionally
    // true on the explicit-prefix path Dependabot uses) — which is
    // Sentence case and fails config-conventional's default subject-case
    // rule. Dropping only "sentence-case" from the disallowed list (kept:
    // start-case, pascal-case, upper-case) accepts Dependabot's format
    // while still catching genuinely malformed subjects — verified
    // locally: "chore(deps): Bump x from 2 to 3" now passes, "feat: Add A
    // New Sleeve" (start-case) still fails.
    "subject-case": [2, "never", ["start-case", "pascal-case", "upper-case"]],
  },
};
