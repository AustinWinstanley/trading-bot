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
    // Dependabot's auto-generated commit bodies routinely exceed 100 chars
    // per line — long "Bumps the X group with N updates: [pkg](url), ..."
    // summary lines and per-package "- [Commits](long-compare-url)" links
    // are not prose meant to be manually wrapped. Confirmed against
    // @commitlint/config-conventional's actual published rules (both are
    // Error-severity, 100-char defaults) that this was the real cause of a
    // PR failure the exact wagoid/commitlint-github-action run reported
    // ("body's lines must not be longer than 100 characters
    // [body-max-line-length]") — disabled both body- and
    // footer-max-line-length rather than just body-, since Dependabot's
    // trailing YAML `updated-dependencies:` block is equally likely to trip
    // the footer variant of the same rule.
    "body-max-line-length": [0],
    "footer-max-line-length": [0],
  },
};
